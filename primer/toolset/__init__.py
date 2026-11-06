"""Concrete ToolsetProvider implementations.

Each implementation subclasses :class:`primer.int.ToolsetProvider` and
exposes one source of tools to the application.

The framework also ships **built-in internal toolsets** assembled from
the Python implementation here. They are immutable (no config row to
delete), and each is constructed by a factory in its
``primer.toolset.<name>`` module:

* ``build_web_toolset`` (``web_search``, ``web_fetch``,
  ``http_request``, and ``download``, which is ``requires_workspace`` and is
  registered only with a ``workspace_registry``, hence rejected from MCP by
  ``is_exposable`` (``needs_workspace``, ``primer/mcp/safety.py``)).
* ``build_system_toolset``, ``build_crud_toolset``,
  ``build_canvas_toolset``, ``build_collections_toolset``,
  ``build_harness_toolset_provider``, ``build_misc_toolset``,
  ``build_trigger_toolset_provider``, ``build_workspace_ext_toolset`` and
  ``build_workspaces_toolset``.
"""

from primer.toolset.internal import InternalToolsetProvider
from primer.toolset.mcp import McpToolsetProvider
from primer.toolset.web import build_web_toolset

__all__ = [
    "InternalToolsetProvider",
    "McpToolsetProvider",
    "build_web_toolset",
]
