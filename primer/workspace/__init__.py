"""Workspace runtime + tool implementations.

This package holds the concrete classes that implement the workspace
contract defined under :mod:`primer.int.workspace`:

* :class:`WorkspaceTool` -- ABC for one workspace-scoped tool
  (``ls``, ``read``, ``write``, ``edit``, ``glob``, ``grep``, ``exec``).
* :class:`ToolCallContext` -- per-call context handed to a tool's
  ``execute()`` by the agent runtime.
* :class:`ToolResult` -- what a workspace tool returns.
* :class:`LocalStateRepo` -- git-backed per-workspace state (host-FS).
* :class:`LocalTruncationStore` -- per-session ``.tmp/`` cache (host-FS).
* :class:`AgentSession` -- per-execution state handle.
* :class:`LocalWorkspace` / :class:`LocalWorkspaceBackend` -- the
  host-FS workspace backend.

See ``docs/superpowers/specs/2026-05-02-workspace-design.md`` and
``docs/superpowers/specs/2026-05-11-workspace-backends-design.md``.
"""

from primer.model.workspace import CommitInfo
from primer.workspace.local.cache import LocalTruncationStore, TruncatedOutput
from primer.workspace.local.state import LocalStateRepo
from primer.workspace.session import AgentSession
from primer.workspace.tool import ToolCallContext, ToolResult, WorkspaceTool


# ToolCallContext carries ``session: "AgentSession"`` as a forward
# reference (declared in tool.py before AgentSession exists). Now that
# AgentSession is imported, finalise the model so callers can construct
# ToolCallContext instances without seeing a PydanticUserError.
ToolCallContext.model_rebuild()


# ``local`` imports the workspace tools (which depend on ToolCallContext
# being fully defined), so it MUST come after the model_rebuild() above.
from primer.workspace.local import LocalWorkspace, LocalWorkspaceBackend  # noqa: E402
from primer.workspace.factory import WorkspaceBackendFactory  # noqa: E402


__all__ = [
    "AgentSession",
    "CommitInfo",
    "LocalStateRepo",
    "LocalTruncationStore",
    "LocalWorkspace",
    "LocalWorkspaceBackend",
    "ToolCallContext",
    "ToolResult",
    "TruncatedOutput",
    "WorkspaceBackendFactory",
    "WorkspaceTool",
]
