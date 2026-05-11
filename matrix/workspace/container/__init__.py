"""Container workspace backend.

Wraps any :class:`ContainerRuntimeAdapter` (Docker / Podman / containerd)
with a :class:`WorkspaceBackend` surface, materialising each workspace
as one long-lived container backed by a named volume.
"""

from matrix.workspace.container.backend import ContainerWorkspaceBackend


__all__ = ["ContainerWorkspaceBackend"]
