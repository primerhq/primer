"""Kubernetes workspace backend.

Wraps a single-replica StatefulSet + PVC per workspace and uses
``kubernetes-asyncio`` to manage Pod lifecycle and exec.
"""

from primer.workspace.k8s.backend import KubernetesWorkspaceBackend
from primer.workspace.k8s.sandbox import K8sSandbox


__all__ = ["K8sSandbox", "KubernetesWorkspaceBackend"]
