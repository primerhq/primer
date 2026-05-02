"""Workspace runtime + tool implementations.

This package holds the concrete classes that implement the workspace
contract defined under :mod:`matrix.int.workspace`:

* :class:`WorkspaceTool` -- ABC for one workspace-scoped tool
  (``ls``, ``read``, ``write``, ``edit``, ``glob``, ``grep``, ``exec``).
* :class:`ToolCallContext` -- per-call context handed to a tool's
  ``execute()`` by the agent runtime.
* :class:`ToolResult` -- what a workspace tool returns.
* :class:`StateRepo` -- git-backed per-workspace state (sub-project B).
* :class:`TruncationStore` -- per-session ``.tmp/`` cache (sub-project B).
* :class:`AgentSession` -- per-execution state handle (sub-project D).

The seven concrete :class:`WorkspaceTool` subclasses ship under
:mod:`matrix.workspace.tools`.

Future sub-projects extend this package with:

* :class:`LocalWorkspaceProvider` (sub-project E) -- first concrete
  backend.

See ``docs/superpowers/specs/2026-05-02-workspace-design.md`` for the
full design.
"""

from matrix.workspace.cache import TruncatedOutput, TruncationStore
from matrix.workspace.session import AgentSession
from matrix.workspace.state import CommitInfo, StateRepo
from matrix.workspace.tool import ToolCallContext, ToolResult, WorkspaceTool


# ToolCallContext carries ``session: "AgentSession"`` as a forward
# reference (declared in tool.py before AgentSession exists). Now that
# AgentSession is imported, finalise the model so callers can construct
# ToolCallContext instances without seeing a PydanticUserError.
ToolCallContext.model_rebuild()


# ``local`` imports the workspace tools (which depend on ToolCallContext
# being fully defined), so it MUST come after the model_rebuild() above.
from matrix.workspace.local import LocalWorkspace, LocalWorkspaceBackend  # noqa: E402
from matrix.workspace.factory import WorkspaceBackendFactory  # noqa: E402


__all__ = [
    "AgentSession",
    "CommitInfo",
    "LocalWorkspace",
    "LocalWorkspaceBackend",
    "StateRepo",
    "ToolCallContext",
    "ToolResult",
    "TruncatedOutput",
    "TruncationStore",
    "WorkspaceBackendFactory",
    "WorkspaceTool",
]
