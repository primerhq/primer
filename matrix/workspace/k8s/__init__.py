"""Kubernetes workspace backend.

Wraps a single-replica StatefulSet + PVC per workspace and uses
``kubernetes-asyncio`` to manage Pod lifecycle and exec.
"""

from matrix.workspace.k8s.backend import KubernetesWorkspaceBackend
from matrix.workspace.k8s.sandbox import K8sSandbox


__all__ = ["K8sSandbox", "KubernetesWorkspaceBackend"]
