"""Concrete ToolsetProvider implementations.

Each implementation subclasses :class:`matrix.int.ToolsetProvider` and
exposes one source of tools to the application.

The framework also ships **built-in internal toolsets** assembled from
the Python implementation here. They are immutable (no config row to
delete) and constructed by per-toolset factories:

* :func:`build_web_toolset` — ``web-search`` + ``http-request``.
"""

from matrix.toolset.internal import InternalToolsetProvider
from matrix.toolset.mcp import McpToolsetProvider
from matrix.toolset.web import build_web_toolset

__all__ = [
    "InternalToolsetProvider",
    "McpToolsetProvider",
    "build_web_toolset",
]
