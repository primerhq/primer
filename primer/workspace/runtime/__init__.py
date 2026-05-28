"""Container runtime adapters (Docker / Podman / containerd).

Used only by :class:`primer.workspace.container.ContainerWorkspaceBackend`.
The K8s backend speaks the K8s API directly via ``kubernetes-asyncio``
and does not go through this layer.
"""
