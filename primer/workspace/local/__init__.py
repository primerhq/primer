"""Local-FS workspace backend.

Splits :class:`LocalWorkspace` and :class:`LocalWorkspaceBackend` across
two modules so each file holds one concrete responsibility. The public
import surface is unchanged: ``from primer.workspace.local import
LocalWorkspace, LocalWorkspaceBackend``.
"""

from primer.workspace.local.backend import LocalWorkspaceBackend
from primer.workspace.local.workspace import LocalWorkspace


__all__ = ["LocalWorkspace", "LocalWorkspaceBackend"]
